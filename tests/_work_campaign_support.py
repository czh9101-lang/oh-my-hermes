"""A host-protocol fixture: dispatch identities and observations, no models."""
from copy import deepcopy
from pathlib import Path


class CampaignHost:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.session = "parent"
        self.supported = True
        self.calls = []
        self.bindings = {}
        self.results = {}
        self.expired = []
        self.route = {"model": "ordinary", "provider": "original"}
        self.tools: tuple[str, ...] = ("read_file", "write_file", "patch", "search_files")
        self.expire_ok = True

    def context(self):
        return self.session

    def capabilities(self):
        return {"binding": "native_per_dispatch" if self.supported else "unbound",
                "leaf_tools": self.tools, "scope_enforced": self.supported,
                "identity_bound": self.supported, "verification_observer": self.supported}

    def dispatch(self, manifest):
        key = manifest["attempt_id"]
        if key not in self.bindings:
            self.calls.append(deepcopy(manifest))
            self.bindings[key] = {"attempt_id": key, "task_id": "host-" + key,
                                  "session_ref": "owner-" + manifest["campaign_id"] if manifest["role"] == "orchestrator" else "leaf-" + key,
                                  "route": deepcopy(manifest["route"]), "state": "bound"}
        return deepcopy(self.bindings[key])

    def observe(self, campaign_id, attempt_id):
        return deepcopy(self.results.get(attempt_id, {}))

    def expire(self, campaign_id, attempt_ids) -> bool:
        if not self.expire_ok:
            return False
        self.expired.extend(attempt_ids)
        for attempt_id in attempt_ids:
            if attempt_id in self.bindings:
                self.bindings[attempt_id]["state"] = "expired"
        return True

    def ordinary_route(self):
        return deepcopy(self.route)
