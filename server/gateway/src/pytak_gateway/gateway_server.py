"""src/pytak_gateway/gateway_server.py

Defines the HTTP server an operator uses to manage the gateway: sites, their nodes, and ending a trip.
Detection's events and health reach the gateway through Fusion (http_monitor.py) and MQTT, not here.

With GATEWAY_TLS_CERT set, every endpoint below, GET /health included, also needs a client certificate
signed by the TAK CA (tls.py). That is checked in the TLS handshake, before any of the auth below.

Endpoints. Everything but GET /health, GET and PUT /sites/{site} and POST /sites/{site}/clear needs the admin
password (HTTP Basic). The PUT needs the site's mission package password instead; the GET and the clear take
either one:
  POST   /sites                                      build a site: the site, its boundary and nodes in one package,
                                                     with its mission package name and password
  GET    /sites/{site}                               the whole site as built: the package an update starts from;
                                                     the admin password, or the site's mission package password
                                                     in the X-Mission-Package-Password header
  PUT    /sites/{site}                               update a site: the whole package again; the mission package
                                                     password in the X-Mission-Package-Password header (the
                                                     admin password alone does not do)
  DELETE /sites/{site}                               remove a site and its nodes; the mission package password
                                                     goes in the X-Mission-Package-Password header
  GET    /unregistered                               Fusion sites, subsystems and nodes seen but not registered
  PUT    /sites/{site}/nodes/{subsystem}/{node}      register or update a node; raises what was held
  POST   /sites/{site}/clear                         one ClearRequest: ends that node's trip; the admin password, or
                                                     the site's mission package password in the
                                                     X-Mission-Package-Password header
  POST   /sites/{site}/emitters                      allow an emitter (a MAC address) at a site: its RF alerts
                                                     trip nothing there, and the trips it caused are ended
  GET    /sites/{site}/emitters                      the site's allowed emitters
  DELETE /sites/{site}/emitters/{emitter}            take one off the list: it trips the site's nodes again
  GET    /health                                     liveness: "up" and nothing else (open)

A node is (subsystem, node_id) under its site. Every auth failure, a wrong mission package password included,
is the same 401 with `WWW-Authenticate: Basic`. A site's CoT carries its own mission package and no other (cot.py).

What a build or an update puts on the map (the site marker, its boundary, its nodes, and later their threat areas)
is also in the site's mission package: the CoT is addressed to it, and TAK adds it because the gateway's connection
is the package's owner (cot.identify_event). So a phone that subscribes to the package sees the site and every
change to it. A node or a boundary an update leaves out, and a threat area that ends, comes out of the package
(CotPublisher.forget, forget_boundary).

"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import cot
from .cot_publisher import CotPublisher, log_sender
from .fusion_mapper import FusionMapper, Policy
from .hash_password import hash_password, load_admin_hash, verify_password
from .mission_package import AtakMissionPackages, MissionPackageError, MissionPackageExists
from .models import (
    NODE_ID,
    SITE_ID,
    AllowEmitter,
    BuildPackage,
    ClearRequest,
    NodeRegistration,
    SiteBuild,
    Subsystem,
    emitter_id,
)
from .registry import Refused, Registry, SiteExists, TrippedNodes
from .tak_connection import TakConnection
from .tls import TLSConfig, TLSReloader

logger = logging.getLogger(__name__)


# GATEWAY_DEBUG=1: log every build and update package as it arrives, one line per node (never a password).
DEBUG_PACKAGES = os.getenv("GATEWAY_DEBUG", "").lower() in ("1", "true", "yes")


def _log_package(verb: str, pkg: BuildPackage) -> None:
    if not DEBUG_PACKAGES:
        return
    lines = [f"debug: {verb} {pkg.site_id} at {pkg.lat:.6f},{pkg.lon:.6f}, {len(pkg.nodes)} nodes, "
             f"boundary {len(pkg.boundary) if pkg.boundary else 0} points"]
    for n in pkg.nodes:
        lines.append(
            f"debug:   {n.subsystem.value}/{n.node_id} at {n.lat:.6f},{n.lon:.6f} range={n.range_m} "
            f"accuracy={n.accuracy_m} watch={list(n.watch) if n.watch else None} stream={n.stream}"
        )
    logger.info("\n".join(lines))

basic = HTTPBasic(auto_error=False)
AdminCredentials = Annotated[HTTPBasicCredentials | None, Depends(basic)]


def unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized", headers={"WWW-Authenticate": "Basic"})


def refused(e: Refused) -> HTTPException:
    """The same shape as FastAPI's own 422, so a caller reads every refusal one way."""
    return HTTPException(422, [{"loc": e.loc, "msg": e.msg, "type": "value_error"}])


class GatewayServer:
    """HTTP server providing the REST API that turns Detection's feeds into CoT for the TAK network."""

    def __init__(
        self,
        registry: Registry,
        tak: TakConnection | None = None,
        admin_hash: str | None = None,
        resend: bool = True,
        *,
        mission_packages: AtakMissionPackages,
    ):
        """
        Initialize the gateway server.

        Args:
            registry: the sites and nodes every request is resolved against
            tak: the connection CoT is sent over. None runs on a laptop alone: CoT goes to the log
            admin_hash: Argon2id hash of the admin password; None means no site can be created
            resend: run the 30-second re-send loop (tests turn it off)
            mission_packages: makes and removes a site's ATAK mission package; always required
        """
        self.registry = registry
        self.tak = tak
        self.admin_hash = admin_hash
        self.resend = resend
        self.mission_packages: AtakMissionPackages = mission_packages
        self.tls: TLSReloader | None = None
        self.gateway = CotPublisher(registry, tak.send_many if tak else log_sender, mission_packages.remove_items)
        self.fusion = FusionMapper(self.gateway.state, Policy.from_env())
        self.app = FastAPI(
            title="TAK Team Gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=self._lifespan
        )

        self._define_paths()

    @classmethod
    def from_env(cls, tak_config: dict[str, str] | None) -> "GatewayServer":
        """The registry from REGISTRY_DB, the admin hash from SITE_ADMIN_HASH, TAK from tak_config."""
        registry = Registry.load_from_env()
        admin_hash = load_admin_hash()
        logger.info(f"gateway up: {len(registry.sites())} sites in the registry")
        if admin_hash is None:
            logger.warning("SITE_ADMIN_HASH is not set: no admin route (sites, nodes, clear) can be used")
        if tak_config is None:
            logger.warning("COT_URL is not set: CoT will be logged, not sent")
        return cls(
            registry,
            TakConnection(tak_config) if tak_config else None,
            admin_hash,
            mission_packages=AtakMissionPackages.from_env(),
        )

    def run(self) -> None:
        """Serve. With GATEWAY_TLS_CERT set (see tls.py) this is mutual TLS against the TAK CA; else plain HTTP."""
        tls_config = TLSConfig.from_env()  # raises on a half-set configuration
        options = {}
        if tls_config is None:
            logger.warning("GATEWAY_TLS_CERT is not set: serving plain HTTP, the admin password travels in the clear")
        else:
            self.tls = TLSReloader(tls_config)
            options["ssl_context_factory"] = self.tls.factory
        uvicorn.run(
            self.app, host=os.getenv("GATEWAY_HOST", "0.0.0.0"), port=int(os.getenv("GATEWAY_PORT", "8000")), **options
        )

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        """Manage FastAPI app lifespan, starting TAK gateway, the re-send loop and the TLS file watcher."""
        logger.info("Starting TAK Gateway HTTP server...")

        if self.tak is not None and not await self.tak.start():
            raise RuntimeError("Failed to start TAK connection")

        task = asyncio.create_task(self.gateway.resend_loop()) if self.resend else None
        tls_task = asyncio.create_task(self.tls.watch()) if self.tls else None
        logger.info("TAK Gateway HTTP server started")
        yield
        logger.info("Shutting down TAK Gateway HTTP server...")
        for t in (task, tls_task):
            if t:
                t.cancel()
        await self.mission_packages.close()

    async def _require_admin(self, creds: AdminCredentials) -> None:
        """HTTP Basic: the password (the username is ignored) must match SITE_ADMIN_HASH."""
        # Argon2id is deliberately slow: keep it off the event loop.
        if creds is None or not await asyncio.to_thread(verify_password, self.admin_hash, creds.password):
            raise unauthorized()

    async def _require_mission_password(self, site_id: str, password: str) -> str:
        """The mission package password of this site, or the same 401 as any other auth failure. Returns the
        package's name. KeyError: no such site."""
        name, hashed = self.registry.mission_package_of(site_id)
        if not await asyncio.to_thread(verify_password, hashed, password):
            raise unauthorized()
        return name

    async def _require_admin_or_mission_password(
        self, site_id: str, creds: AdminCredentials, mission_package_password: str
    ) -> None:
        """Either the admin password, or the mission package password of this site and of no other: whoever
        follows a site holds that one, and may end its trips. Anything else is the same 401. KeyError: no such
        site, and no admin password to say so to."""
        if self.admin_hash is not None and creds is not None:
            if await asyncio.to_thread(verify_password, self.admin_hash, creds.password):
                return
        if not mission_package_password:
            raise unauthorized()
        await self._require_mission_password(site_id, mission_package_password)

    async def _announce(self, site_id: str, cots: list[bytes]) -> None:
        """After a build or update has committed: the site and its nodes at once, not at the next re-send, so a
        node the admin moved is seen to move when the update lands; then what was held for its new nodes. Only
        nodes the gateway has state for are sent, as on a re-send. A failed send is only logged: the next
        re-send carries it all."""
        now = datetime.now(timezone.utc)
        site = next(((lat, lon) for sid, lat, lon in self.registry.sites() if sid == site_id), None)
        events = []
        if site is not None:
            mission = self.registry.mission_packages().get(site_id)
            events.append(
                cot.site_event(site_id, *site, now, alert=self.gateway.state.site_alert(site_id), mission=mission)
            )
            boundary = self.registry.boundary(site_id)
            if boundary:
                events.append(cot.boundary_event(site_id, boundary, now, mission=mission))
            events += [cot.node_event(s, now) for s in self.gateway.state.snapshot() if s.node.site_id == site_id]
            events += [cot.node_event(s, now) for s in self.gateway.state.unplaced_snapshot(site_id)]
        events += cots
        if events:
            try:
                await self.gateway.sender(*events)
            except (OSError, RuntimeError) as e:
                logger.warning("could not send site %s to TAK: %s", site_id, e)

    def _adopt(self, site_id: str, session) -> list[bytes]:
        """In a build's or update's transaction: the nodes it places that were showing as unplaced take their state
        with them (GatewayState.adopt_unplaced), ones a placed "*" node now covers are dropped, and every other node
        Fusion reports at the site is listed as unplaced. The CoT to send once it has committed: each moved node and
        the threat area of one that is tripped, and a delete for each dropped one."""
        now = datetime.now(timezone.utc)
        out = []
        state = self.gateway.state
        for new in state.adopt_unplaced(site_id, session, now):
            out.append(cot.node_event(new, now))
            if new.threat is not None:
                out.append(cot.threat_event(new.node, new.threat, new.updated, now))
        # Covered now by a placed "*" node: off the pane. A delete takes it off phones; _announce does not send it.
        out += [cot.delete_event(cot.node_uid(n), now) for n in state.covered_unplaced(site_id, session)]
        state.seed_unplaced(site_id, session, now)  # every other node Fusion reports here: listed at once
        return out

    def _define_paths(self):
        """Define FastAPI route handlers."""
        admin = Depends(self._require_admin)

        @self.app.post("/sites", status_code=201, dependencies=[admin])
        async def build_site(body: SiteBuild) -> dict:
            """The build package (Gateway Changes Spec, change 1): the site, its boundary and its nodes, all or
            nothing, and the mission package it belongs to (the password is kept only as a hash). `nodes` counts
            the nodes taken, so the caller can tell this gateway took the whole package."""
            _log_package("POST build", body)
            hashed = await asyncio.to_thread(hash_password, body.mission_package_password)  # slow: off the loop
            try:
                with self.registry.begin() as s:
                    nodes = self.registry.build_site(body, s, (body.mission_package_name, hashed))
                    cots = self._adopt(body.site_id, s) + self.fusion.replay(s, datetime.now(timezone.utc))
            except SiteExists:
                raise HTTPException(status.HTTP_409_CONFLICT, "site id is taken") from None
            except Refused as e:
                raise refused(e) from None

            try:
                await self.mission_packages.create(body.mission_package_name, body.mission_package_password)
            except MissionPackageError as e:
                self.registry.delete_site(body.site_id)  # nothing half built: no site without its mission package
                logger.warning("site %s not built: mission package %r: %s", body.site_id, body.mission_package_name, e)
                if isinstance(e, MissionPackageExists):
                    raise HTTPException(status.HTTP_409_CONFLICT, "mission package already exists") from None
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not create the mission package") from None

            logger.info(
                "site %s built with %d nodes, mission package %r", body.site_id, len(nodes), body.mission_package_name
            )
            await self._announce(body.site_id, cots)
            return {"site": body.site_id, "nodes": len(nodes), "mission_package": body.mission_package_name}

        @self.app.get("/sites/{site}")
        async def get_site(
            site: Annotated[str, Path(pattern=SITE_ID)],
            creds: AdminCredentials,
            mission_package_password: Annotated[str, Header(alias="X-Mission-Package-Password")] = "",
        ) -> dict:
            """The whole site as its last build or update left it, in the build package's shape, with its mission
            package's name and never its password. Takes the admin password, or this site's mission package
            password (X-Mission-Package-Password header): whoever holds that one may edit the site on any phone,
            and PUT it back whole with it."""
            try:
                await self._require_admin_or_mission_password(site, creds, mission_package_password)
            except KeyError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site") from None
            package = self.registry.site_package(site)
            if package is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site")
            return package

        @self.app.put("/sites/{site}")
        async def update_site(
            site: Annotated[str, Path(pattern=SITE_ID)],
            body: BuildPackage,
            mission_package_password: Annotated[str, Header(alias="X-Mission-Package-Password")] = "",
        ) -> dict:
            """The whole site again. Needs the site's mission package password (X-Mission-Package-Password
            header, as on delete) and not the admin password. The mission package's name is fixed when the site
            is built, so the body cannot carry one. A node left out is removed and taken off the map, unless it
            is tripped: then nothing changes and the reply names it."""
            _log_package("PUT update", body)
            if body.site_id != site:
                raise HTTPException(
                    422, [{"loc": ["body", "site_id"], "msg": "is not the site in the address", "type": "value_error"}]
                )
            try:
                mission = await self._require_mission_password(site, mission_package_password)
            except KeyError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site") from None
            had_boundary = self.registry.boundary(site) is not None
            try:
                with self.registry.begin() as s:
                    nodes, gone = self.registry.update_site(site, body, s)
                    cots = self._adopt(site, s) + self.fusion.replay(s, datetime.now(timezone.utc))
            except KeyError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site") from None
            except Refused as e:
                raise refused(e) from None
            except TrippedNodes as e:
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content={
                        "detail": f"would remove tripped nodes: {', '.join(e.nodes)}; clear them first",
                        "tripped": e.nodes,
                    },
                )
            await self.gateway.forget(gone)
            if had_boundary and not body.boundary:
                await self.gateway.forget_boundary(site, mission)
            logger.info("site %s updated: %d nodes, %d removed", site, len(nodes), len(gone))
            await self._announce(site, cots)
            return {"site": site, "nodes": len(nodes), "removed": len(gone)}

        @self.app.delete("/sites/{site}", dependencies=[admin])
        async def delete_site(
            site: Annotated[str, Path(pattern=SITE_ID)],
            mission_package_password: Annotated[str, Header(alias="X-Mission-Package-Password")] = "",
        ) -> dict:
            """Needs the admin password and the site's mission package password. The mission package is removed
            first: if that fails the site stays."""
            try:
                name = await self._require_mission_password(site, mission_package_password)
            except KeyError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site") from None
            try:
                await self.mission_packages.remove(name, mission_package_password)
            except MissionPackageError as e:
                logger.warning("site %s not deleted: mission package %r: %s", site, name, e)
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not remove the mission package") from None
            # Its unplaced nodes go with the site, and off the map too.
            unplaced = [st.node for st in self.gateway.state.unplaced_snapshot(site)]
            gone = self.registry.delete_site(site)
            if gone is None:  # deleted by another request meanwhile
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site")
            gone += unplaced
            await self.gateway.forget(gone, site_id=site, mission=name)
            logger.info("site %s deleted with %d nodes", site, len(gone))
            return {"site": site, "nodes_removed": len(gone)}

        @self.app.get("/unregistered", dependencies=[admin])
        async def unregistered() -> dict:
            """The drop-down: what Fusion has sent for that nobody has registered, as sites, their
            subsystems and, under them, their nodes."""
            return {"sites": self.registry.list_unregistered()}

        @self.app.put("/sites/{site}/nodes/{subsystem}/{node}", dependencies=[admin])
        async def register_node(
            site: Annotated[str, Path(pattern=SITE_ID)],
            subsystem: Subsystem,
            node: Annotated[str, Path(pattern=NODE_ID)],
            body: NodeRegistration,
            response: Response,
        ) -> dict:
            """Safe to call again: the same node under the same site updates it. One transaction:
            the node, and the detections held for it raised. Only once that has committed does anything go
            to TAK."""
            try:
                with self.registry.begin() as s:
                    registered, created = self.registry.register_node(site, subsystem, node, body, session=s)
                    cots = self.fusion.replay(s, datetime.now(timezone.utc))
            except KeyError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site") from None
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
            if cots:
                try:
                    await self.gateway.sender(*cots)
                except (OSError, RuntimeError) as e:  # committed: the next resend carries it
                    logger.warning("could not send a raised detection to TAK: %s", e)
            response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
            return {
                "site": site,
                "subsystem": subsystem.value,
                "node": node,
                "uid": cot.node_uid(registered),
                "created": created,
                "raised": sum(b'role="node"' in c for c in cots),
            }

        @self.app.post("/sites/{site}/clear", status_code=202)
        async def post_clear(
            site: Annotated[str, Path(pattern=SITE_ID)],
            c: ClearRequest,
            creds: AdminCredentials,
            mission_package_password: Annotated[str, Header(alias="X-Mission-Package-Password")] = "",
        ) -> dict:
            """Ends the trip of one of that site's nodes, placed or not (an unplaced node is shown in the pane).
            Takes the admin password, or this site's mission package password (X-Mission-Package-Password header,
            as on update): a phone that follows the site may end its trips, and holds no other credential."""
            try:
                await self._require_admin_or_mission_password(site, creds, mission_package_password)
            except KeyError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site") from None
            unplaced = any(
                st.node.subsystem == c.subsystem and st.node.node_id == c.node_id
                for st in self.gateway.state.unplaced_snapshot(site)
            )
            if self.registry.resolve(site, c.subsystem, c.node_id) is None and not unplaced:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such node")
            new = await self.gateway.handle_clear(site, c)
            if new is None:
                return {"accepted": False, "reason": "not tripped, stale, or not the trip named"}
            return {"accepted": True, "status": new.status.value}

        @self.app.post("/sites/{site}/emitters", dependencies=[admin])
        async def allow_emitter(
            site: Annotated[str, Path(pattern=SITE_ID)], body: AllowEmitter, response: Response
        ) -> dict:
            """Allows an emitter at a site: from now on an RF alert it causes there trips nothing. The trips it
            has caused there, still open, are ended in the same transaction: the admin has just said it is
            friendly. Safe to call again. Needs the admin password; the mission package password opens none of
            the allow list, since it would let any phone that follows the site switch RF detection off."""
            try:
                with self.registry.begin() as s:
                    created = self.registry.allow_emitter(site, body.emitter, body.name, s)
                    cleared = self.gateway.state.clear_emitter(site, body.emitter, s)
            except KeyError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site") from None
            except Refused as e:
                raise refused(e) from None
            logger.info(
                "site %s: emitter %s allowed (%s), %d trips ended",
                site,
                body.emitter,
                "added" if created else "already listed",
                len(cleared),
            )
            await self.gateway.handle_allow(cleared)
            response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
            return {"site": site, "emitter": body.emitter, "created": created, "cleared": len(cleared)}

        @self.app.get("/sites/{site}/emitters", dependencies=[admin])
        async def allowed_emitters(site: Annotated[str, Path(pattern=SITE_ID)]) -> dict:
            """The site's allow list: each emitter, the admin's name for it if it has one, and when it was added."""
            try:
                return {"site": site, "emitters": self.registry.allowed_emitters(site)}
            except KeyError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such site") from None

        @self.app.delete("/sites/{site}/emitters/{emitter}", dependencies=[admin])
        async def disallow_emitter(
            site: Annotated[str, Path(pattern=SITE_ID)], emitter: Annotated[str, Path(max_length=32)]
        ) -> dict:
            """Takes an emitter off the site's allow list: its next RF alert there trips a node again. Nothing
            that is tripped or clear now changes."""
            mac = emitter_id(emitter)
            if mac is None or not self.registry.disallow_emitter(site, mac):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such emitter on this site's list")
            logger.info("site %s: emitter %s no longer allowed", site, mac)
            return {"site": site, "emitter": mac}

        @self.app.get("/health")
        async def liveness() -> dict:
            return {"status": "up"}
