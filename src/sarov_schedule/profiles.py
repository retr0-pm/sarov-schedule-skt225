from dataclasses import dataclass

from .rules import RULES_VERSION


@dataclass(frozen=True)
class Profile:
    name: str
    group: str
    course: int
    rules_version: str

    @property
    def calendar_title(self) -> str:
        return f"Расписание {self.group}"

    @property
    def managed_by(self) -> str:
        return f"sarov-schedule-{self.name}"


SKT225 = Profile("skt225", "СКТ-225", 2, RULES_VERSION)
VM126 = Profile("vm126", "ВМ-126", 1, "vm126-v1")
PROFILES = {profile.name: profile for profile in (SKT225, VM126)}
