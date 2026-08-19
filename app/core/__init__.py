"""Core cargo processing modules."""

def __getattr__(name: str):
    if name in ("CargoNormalizer", "default_normalizer"):
        from app.core import normalizer
        return getattr(normalizer, name)
    if name in ("CargoValidator", "default_validator"):
        from app.core import validator
        return getattr(validator, name)
    if name in ("SkillRunner", "default_skill_runner"):
        from app.core import skill_runner
        return getattr(skill_runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "CargoNormalizer",
    "default_normalizer",
    "CargoValidator",
    "default_validator",
    "SkillRunner",
    "default_skill_runner",
]
