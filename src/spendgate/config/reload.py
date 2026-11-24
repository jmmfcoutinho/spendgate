"""Read the `file:` secrets of loaded settings again, for a running server (SIGHUP).

Only secret values change. Routes, deployments and every other setting stay as loaded: a rotated
key or password is new file content, a new route is a restart. `env:` values cannot change inside
a running process, so they are not read again. The key pepper is not reloaded either: a new pepper
would invalidate every key at once. A secret that cannot be read now keeps its old value, so a
rotation caught half-written does not take a working deployment down.
"""

from dataclasses import dataclass, field, replace

from spendgate.config.load import credential_path
from spendgate.config.schema import Provider
from spendgate.config.secrets import Secret, resolve
from spendgate.config.settings import ResolvedDeployment, Route, Settings

_FILE = "file:"


@dataclass(frozen=True, slots=True)
class Reread:
    """New settings plus what happened, by config path (never a value)."""

    settings: Settings
    reloaded: tuple[str, ...]  # every `file:` secret read again
    changed: tuple[str, ...]  # of those, the ones whose value is new
    failed: tuple[tuple[str, str], ...]  # (path, reason); the old value stays in use


@dataclass(slots=True)
class _Pass:
    read: dict[str, Secret | None] = field(default_factory=dict)  # path -> new value, None: failed
    changed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def fresh(self, ref: str | None, path: str, old: Secret | None) -> Secret | None:
        """The value of `ref` read again when it is a file, else `old`; `old` on failure too."""
        if ref is None or not ref.startswith(_FILE):
            return old
        if path not in self.read:
            try:
                new: Secret | None = resolve(ref, {})
            except LookupError as exc:  # names the file, never its content
                self.failed.append((path, str(exc)))
                new = None
            self.read[path] = new
            if new is not None and new != old:
                self.changed.append(path)
        new = self.read[path]
        return old if new is None else new


def _route(settings: Settings, alias: str, route: Route, run: _Pass) -> Route:
    cfg = settings.config
    specs = cfg.models[alias].deployments
    out: list[ResolvedDeployment] = []
    # A loaded Settings holds one resolved deployment per spec, in order (any gap is a ConfigError).
    for i, (spec, built) in enumerate(zip(specs, route.deployments, strict=True)):
        ref = spec.credential or cfg.providers.get(spec.provider, Provider()).credential
        where = credential_path(spec, f"models.{alias}.deployments.{i}")
        new = run.fresh(ref, where, built.credential)
        out.append(built if new is built.credential else replace(built, credential=new))
    return replace(route, deployments=tuple(out))


def reread_file_secrets(settings: Settings) -> Reread:
    """`settings` with every `file:` provider credential and the ledger DSN read again."""
    run = _Pass()
    routes = {alias: _route(settings, alias, r, run) for alias, r in settings.routes.items()}
    backend = settings.config.ledger.backend
    dsn = settings.ledger_dsn
    if backend.kind == "postgres":
        dsn = run.fresh(backend.dsn, "ledger.backend.dsn", dsn)
    reloaded = tuple(p for p, v in run.read.items() if v is not None)
    new = replace(settings, routes=routes, ledger_dsn=dsn)
    return Reread(new, reloaded, tuple(run.changed), tuple(run.failed))
