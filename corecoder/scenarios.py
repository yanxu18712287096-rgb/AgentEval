"""Small in-process contracts, not a plugin loader or a skill router."""

from dataclasses import asdict, dataclass
import copy
import hashlib
import json
from typing import Callable


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class SkillSpec:
    id: str
    version: str
    content: str
    compatible_roles: tuple[str, ...]

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.id, self.version, self.content)):
            raise ValueError("skill identity, version and content are required")


@dataclass(frozen=True)
class RoleSpec:
    id: str
    version: str
    prompt: str
    allowed_tools: tuple[str, ...]
    skills: tuple[SkillSpec, ...] = ()

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.id, self.version, self.prompt)):
            raise ValueError("role identity, version and prompt are required")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("duplicate allowed tools")

    def render(self):
        if len({s.id for s in self.skills}) != len(self.skills):
            raise ValueError("duplicate skill IDs")
        for skill in self.skills:
            if self.id not in skill.compatible_roles:
                raise ValueError(f"skill {skill.id} is incompatible with role {self.id}")
        return self.prompt + "".join(f"\n\n# Skill: {s.id}@{s.version}\n{s.content}" for s in self.skills)


@dataclass(frozen=True)
class CaseSpec:
    """Own a defensive copy; business assertions remain scenario-specific."""

    data: dict

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("case must be an object")
        case = copy.deepcopy(value)
        if not isinstance(case.get("id"), str) or not case["id"]:
            raise ValueError("case ID is required")
        if case.get("split") not in {"development", "holdout"}:
            raise ValueError("invalid case split")
        messages = case.get("messages")
        if not isinstance(messages, list) or not messages or any(
                not isinstance(m, dict) or m.get("role") != "user"
                or not isinstance(m.get("content"), str) for m in messages):
            raise ValueError("case messages must be user turns")
        for key in ("fixture", "expect", "route"):
            if not isinstance(case.get(key), dict):
                raise ValueError(f"case {key} must be an object")
        for key, minimum in (("max_model_rounds", 1), ("max_tool_calls", 0)):
            value = case["route"].get(key)
            if type(value) is not int or value < minimum:
                raise ValueError("invalid route budget")
        # Explicit compatibility adapter for pre-interface order cases.
        case.setdefault("scenario", "orders")
        case.setdefault("scenario_version", "1")
        if any(not isinstance(case[k], str) or not case[k] for k in ("scenario", "scenario_version")):
            raise ValueError("scenario identity and version must be strings")
        return cls(case)


@dataclass(frozen=True)
class ScenarioSpec:
    id: str
    version: str
    role: RoleSpec
    state_factory: Callable
    tools_factory: Callable
    validate: Callable
    scorer: Callable
    snapshot: Callable
    restore: Callable
    scorer_version: str
    implementation_files: tuple[str, ...] = ()
    turn_setup: Callable | None = None
    resource_files: tuple[str, ...] = ()

    def create(self, fixture):
        state = self.state_factory(copy.deepcopy(fixture))
        tools = self.tools_factory(state)
        names = [t.name for t in tools]
        if len(names) != len(set(names)) or set(names) != set(self.role.allowed_tools):
            raise ValueError("tool factory does not match role allowlist")
        self.role.render()
        return state, tools

    def identity(self):
        return json.loads(json.dumps({"id": self.id, "version": self.version,
                                     "role": asdict(self.role), "role_hash": content_hash(asdict(self.role)),
                                     "skill_hashes": {s.id: content_hash(asdict(s)) for s in self.role.skills},
                                     "scorer_version": self.scorer_version}))


class ScenarioRegistry:
    def __init__(self):
        self._items = {}

    def register(self, scenario):
        key = (scenario.id, scenario.version)
        if key in self._items:
            raise ValueError(f"duplicate scenario: {key}")
        self._items[key] = scenario

    def resolve(self, case):
        spec = CaseSpec.from_dict(case)
        key = (spec.data["scenario"], spec.data["scenario_version"])
        if key not in self._items:
            raise ValueError(f"unknown scenario: {key}")
        return self._items[key]
