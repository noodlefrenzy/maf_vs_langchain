"""Role-authenticated Streamable HTTP service over isolated run worlds."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from agent_lab.contracts import FrameworkLane
from incident_lab_mcp.http_auth import (
    CredentialError,
    RunCredentialSigner,
    RunWorld,
    RunWorldRegistry,
)
from incident_lab_mcp.roles import ROLES, Role
from incident_lab_mcp.server import build_server


class ExpectedRunTokenVerifier:
    """MCP TokenVerifier bound to exactly one run, lane, and role."""

    def __init__(
        self,
        *,
        registry: RunWorldRegistry,
        signer: RunCredentialSigner,
        run: RunWorld,
        role: Role,
        resource_url: str,
    ) -> None:
        self._registry = registry
        self._signer = signer
        self._run = run
        self._role = role
        self._resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = self._signer.verify(token)
            active_run = self._registry.get(claims.run_id)
        except (CredentialError, KeyError, ValueError):
            return None
        if (
            claims.run_id != self._run.run_id
            or claims.lane is not self._run.lane
            or claims.role != self._role
            or active_run is not self._run
        ):
            return None
        scope = _role_scope(self._role)
        return AccessToken(
            token=token,
            client_id=f"agent-lab:{claims.run_id}:{claims.role}",
            scopes=[scope],
            expires_at=claims.expires_at,
            resource=self._resource_url,
            subject=str(claims.run_id),
            claims=claims.model_dump(mode="json"),
        )


def build_http_app(
    registry: RunWorldRegistry,
    signer: RunCredentialSigner,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> Starlette:
    """Mount one authenticated role catalog for every pre-created run."""

    if host != "127.0.0.1":
        raise ValueError("incident HTTP service must bind to 127.0.0.1")
    active_runs = registry.active_runs()
    if not active_runs:
        raise ValueError("at least one active run world is required")

    child_apps: list[Starlette] = []
    routes: list[Route | Mount] = [
        Route("/healthz", endpoint=_health),
        Route("/readyz", endpoint=_readiness(registry)),
    ]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
        allowed_origins=[
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ],
    )
    for run in sorted(active_runs, key=lambda item: str(item.run_id)):
        for role in ROLES:
            mount_path = f"/runs/{run.run_id}/{role}"
            resource_url = f"http://{host}:{port}{mount_path}"
            verifier = ExpectedRunTokenVerifier(
                registry=registry,
                signer=signer,
                run=run,
                role=role,
                resource_url=resource_url,
            )
            auth = AuthSettings(
                issuer_url=f"http://{host}:{port}/local-auth",
                resource_server_url=resource_url,
                required_scopes=[_role_scope(role)],
            )
            mcp_server = build_server(
                role,
                seed=run.world.incident.seed,
                world=run.world,
                token_verifier=verifier,
                auth=auth,
                transport_security=security,
            )
            child = mcp_server.streamable_http_app()
            child_apps.append(child)
            routes.append(Mount(mount_path, app=child))

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for child in child_apps:
                await stack.enter_async_context(child.router.lifespan_context(child))
            yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.registry = registry
    app.state.signer = signer
    return app


def issue_endpoint_credentials(
    registry: RunWorldRegistry,
    signer: RunCredentialSigner,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> dict[str, Any]:
    """Issue one credential and MCP URL per active run/role combination."""

    credentials = []
    for run in sorted(registry.active_runs(), key=lambda item: str(item.run_id)):
        for role in ROLES:
            credentials.append(
                {
                    "run_id": str(run.run_id),
                    "pair_id": run.pair_id,
                    "lane": run.lane.value,
                    "role": role,
                    "expires_at": run.expires_at,
                    "url": f"http://{host}:{port}/runs/{run.run_id}/{role}/mcp",
                    "token": signer.issue(run, role),
                }
            )
    return {"schema_version": "0.1.0", "credentials": credentials}


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _readiness(registry: RunWorldRegistry):
    async def endpoint(_request: Request) -> JSONResponse:
        active = registry.active_runs()
        status = 200 if active else 503
        return JSONResponse(
            {"status": "ready" if active else "not_ready", "active_runs": len(active)},
            status_code=status,
        )

    return endpoint


def _role_scope(role: Role) -> str:
    return f"incident-lab:role:{role}"


def _write_credentials(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--pair-id", default="development-pair")
    parser.add_argument(
        "--left-lane",
        type=FrameworkLane,
        choices=list(FrameworkLane),
        default=FrameworkLane.MAF_WORKFLOW,
    )
    parser.add_argument(
        "--right-lane",
        type=FrameworkLane,
        choices=list(FrameworkLane),
        default=FrameworkLane.LANGGRAPH,
    )
    parser.add_argument(
        "--fault-profile",
        choices=["none", "standard", "adversarial"],
        default="standard",
    )
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args()

    registry = RunWorldRegistry()
    receipt = registry.create_pair(
        pair_id=args.pair_id,
        seed=args.seed,
        left_lane=args.left_lane,
        right_lane=args.right_lane,
        ttl_seconds=args.ttl_seconds,
        fault_profile=args.fault_profile,
    )
    signer = RunCredentialSigner()
    credentials = issue_endpoint_credentials(
        registry,
        signer,
        port=args.port,
    )
    credentials["pair"] = receipt.model_dump(mode="json")
    _write_credentials(args.credential_file, credentials)
    app = build_http_app(registry, signer, port=args.port)

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
