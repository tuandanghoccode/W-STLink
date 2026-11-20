"""Persist each target and its watch list as a named profile."""

import json


class ProfileStore:
    def __init__(self, path):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}

        profiles = data.get("profiles")
        if isinstance(profiles, dict):
            self.profiles = {name: value for name, value in profiles.items() if name and isinstance(value, dict)}
        else:
            self.profiles = {"Mặc định": data}
        if not self.profiles:
            self.profiles = {"Mặc định": {}}
        active = data.get("active")
        self.active = active if isinstance(active, str) and active in self.profiles else next(iter(self.profiles))

    def save(self, active, settings):
        profiles = dict(self.profiles)
        profiles[active] = settings
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"active": active, "profiles": profiles}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.profiles = profiles
        self.active = active
