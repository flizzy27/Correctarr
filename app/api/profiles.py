"""Quality profiles: building one, writing it, and moving titles onto it."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import i18n, sizing
from .. import profiles as vprofiles
from ..arr import ArrError
from ..rules import check_profile_loop
from . import core
from .core import fail, language_for, log, require_user
from .library import (
    arr_entries,
    arr_entry,
    arr_for,
    forget_libraries,
    library_of,
    service_summary,
)

router = APIRouter()


class ProfileWish(BaseModel):
    """What the page asks for. Everything else is worked out from it."""
    name: str = Field(default="Correctarr 1080p", max_length=60)
    resolutions: list[str] = Field(default_factory=lambda: ["1080p"])
    sources: list[str] = Field(default_factory=lambda: ["webdl", "bluray"])
    audio: str = "gut"
    surround: bool = True
    codec: str = "any"
    languages: list[str] = Field(default_factory=lambda: ["de"])
    language_required: bool = False
    colour: str = "sdr"
    edition: str = "none"
    streamers: list[str] = Field(default_factory=list)
    good_groups: bool = True
    prefer_repack: bool = True
    allow_3d: bool = False
    block_rubbish: bool = True
    block_hardcoded_subs: bool = True
    block_retagged: bool = True
    block_collections: bool = True
    min_gb: float = 0.0
    max_gb: float = 0.0
    upgrade: bool = True
    #: What the switch beside the resolutions used to be. Remux is a source
    #: now; a page cached from an older build still sends this.
    allow_remux: bool = False
    #: Sources for one resolution where it should differ from ``sources``.
    ladder: dict[str, list[str]] = Field(default_factory=dict)
    #: A size window per resolution: ``{"2160p": {"min_gb": 0, "max_gb": 60}}``.
    size_limits: dict[str, dict[str, float]] = Field(default_factory=dict)
    codec_required: bool = False
    upgrade_until: str = ""
    #: Which services to write it to. Empty means every Radarr and Sonarr.
    services: list[int] = Field(default_factory=list)
    #: The preview only: work out what this profile would take on the disk of
    #: this service, for the titles of ``estimate_profile`` or, without one,
    #: for the whole library.
    estimate_for: int | None = None
    estimate_profile: int | None = None

    def wish(self) -> vprofiles.Wish:
        return vprofiles.wish_from(self.model_dump())


@router.get("/api/profiles/options")
def profile_options(_: dict = Depends(require_user)):
    """Everything the page needs to draw itself, and where it can send one."""
    return {
        "resolutions": list(vprofiles.RESOLUTIONS),
        "sources": list(vprofiles.SOURCES),
        "audio": list(vprofiles.AUDIO_TIERS),
        "codecs": list(vprofiles.CODECS),
        "languages": list(vprofiles.LANGUAGES),
        "ranges": list(vprofiles.RANGES),
        "editions": list(vprofiles.EDITIONS),
        "streamers": list(vprofiles.STREAMERS),
        "mark": vprofiles.MARK,
        "services": [service_summary(entry) for entry in arr_entries()],
    }


def _described(blueprint, language: str) -> dict:
    """A blueprint with every reason and warning already in words."""
    out = blueprint.as_dict()
    for entry, source in zip(out["formats"], blueprint.formats, strict=True):
        params = dict(source.why_params)
        # The sound step is carried as a key. Putting the key in front of a
        # person is how "sehr_gut" ends up in the middle of a German sentence.
        if "tier" in params:
            label = i18n.t(f"profiles_page.audio_{params['tier']}", language)
            params["tier"] = label.split("—")[0].strip()
        # Same for the language: "wanted in DE" is a code, not a language.
        if "language" in params:
            params["language"] = i18n.t(
                f"language.{str(params['language']).lower()}", language)
        for key, prefix in (("source", "profiles_page.source_"),
                            ("edition", "profiles_page.edition_")):
            if key in params:
                params[key] = i18n.t(f"{prefix}{params[key]}", language)
        entry["why"] = i18n.t(source.why, language, **params) if source.why else ""
    out["notes"] = [i18n.t(key, language, **params)
                    for key, params in blueprint.notes]
    out["problems"] = [i18n.t(key, language, **params)
                       for key, params in vprofiles.check(blueprint)]
    return out


@router.post("/api/profiles/preview")
def preview_profile(body: ProfileWish, request: Request,
                    _: dict = Depends(require_user)):
    """What would be created, before anything is.

    Nothing is written. The point is that a profile is a set of rules somebody
    is going to let loose on their library, and being able to read it first is
    the difference between trusting it and hoping.

    With ``estimate_for`` it also says what the profile would take on that
    service's disk, which costs a read of the library — hence only on request.
    """
    language = language_for(request)
    blueprint = vprofiles.build(body.wish())
    out = _described(blueprint, language)
    out["estimate"] = None
    if body.estimate_for is not None:
        entry = arr_entry(request, body.estimate_for)
        snapshot = library_of(entry)
        wish = blueprint.wish
        keys = vprofiles.ladder_keys(wish)
        chosen = [t for t in snapshot["titles"]
                  if body.estimate_profile is None
                  or t.profile_id == body.estimate_profile]
        cutoff = (f"{wish.upgrade_until}-{wish.resolutions[-1]}"
                  if wish.upgrade_until else None)
        out["estimate"] = _estimate(
            snapshot, entry, chosen, keys, cutoff,
            sizing.wish_limits(wish, keys), wish.upgrade, language,
            scope=body.estimate_profile)
    return out


def _estimate(snapshot: dict, entry: dict, chosen: list, keys: list[str],
              cutoff: str | None, limits: dict, upgrade: bool, language: str,
              *, scope: int | None) -> dict:
    """A profile against part of a library, with what the numbers rest on."""
    table = snapshot["rates"]
    minutes = sizing.example_minutes(entry["kind"])
    example = sizing.estimate(minutes, keys, table, limits)
    result = sizing.library(chosen, keys, cutoff, table, limits, upgrade=upgrade)
    free = sizing.free_space(snapshot["roots"], chosen)
    counts = result["counts"]
    warnings: list[tuple[str, dict]] = []
    if example is None:
        warnings.append(("storage.warning.nothing_to_measure", {}))
    elif example.capped:
        warnings.append(("storage.warning.capped", {
            "quality": sizing.label(example.quality),
            "gb": f"{table[example.quality].typical * minutes / 1024:.1f}"}))
    if counts["without_runtime"]:
        warnings.append(("storage.warning.no_runtime", {
            "count": counts["without_runtime"],
            "minutes": sizing.FILM_MINUTES if entry["kind"] == "radarr"
            else sizing.EPISODE_MINUTES}))
    if counts["assumed_kept"]:
        warnings.append(("storage.warning.assumed_kept",
                         {"count": counts["assumed_kept"]}))
    return {
        "service": service_summary(entry),
        "scope": {"profile_id": scope, "titles": len(chosen)},
        "example": example.as_dict(minutes) if example else None,
        "qualities": [table[key].as_dict() for key in keys if key in table],
        **result,
        "free_gb": free, "fits": sizing.fits(result["change"], free),
        "warnings": [{"key": key, "params": params,
                      "text": i18n.t(key, language, **params)}
                     for key, params in warnings],
    }


@router.post("/api/profiles/apply")
def apply_profile(body: ProfileWish, request: Request,
                  _: dict = Depends(require_user)):
    """Write the profile and its formats into the chosen services."""
    blueprint = vprofiles.build(body.wish())
    problems = vprofiles.check(blueprint)
    if problems:
        key, params = problems[0]
        raise HTTPException(400, i18n.t(key, language_for(request), **params))

    wanted = set(body.services)
    entries = [e for e in arr_entries() if not wanted or e["id"] in wanted]
    if not entries:
        raise fail(request, 400, "error.no_services")

    done, failures = [], []
    for entry in entries:
        connector = arr_for(entry)
        try:
            done.append(vprofiles.apply_to(connector, blueprint))
        except (ArrError, ValueError) as e:
            # A refusal from the builder arrives as a translation key, and a
            # key is not something to put in front of a person.
            error = str(e)
            if isinstance(e, ValueError) and error.startswith("profiles.problem."):
                error = i18n.t(error, language_for(request))
            failures.append({"service": entry["name"], "error": error[:300]})
            log.warning("Could not write the profile to %s: %s",
                        entry["name"], e)
        finally:
            connector.close()
    forget_libraries()
    if not done:
        raise HTTPException(502, "; ".join(
            f["service"] + ": " + f["error"] for f in failures) or "failed")
    return {"ok": True, "written": done, "failed": failures,
            "blueprint": _described(blueprint, language_for(request))}


@router.get("/api/profiles/presets")
def profile_presets(request: Request, languages: str = "",
                    required: bool = False, service_id: int | None = None,
                    _: dict = Depends(require_user)):
    """Ready profiles to pick from, each with its whole wish.

    The wish is what the advanced form is filled with when a preset is opened
    there, so a preset is a starting point rather than a dead end. The size
    example is from the reference table, or measured against a library when
    ``service_id`` names one.
    """
    language = language_for(request)
    codes = tuple(code.strip().lower() for code in languages.split(",") if code.strip())
    measured = None
    if service_id is not None:
        entry = arr_entry(request, service_id)
        measured = (entry, library_of(entry))
    reference = sizing.rates({}, {})

    out = []
    for preset in vprofiles.PRESETS:
        wish = vprofiles.preset_wish(preset, codes, required)
        blueprint = vprofiles.build(wish)
        keys = vprofiles.ladder_keys(wish)
        limits = sizing.wish_limits(wish, keys)
        examples = []
        for kind in preset.services:
            table = (measured[1]["rates"]
                     if measured and measured[0]["kind"] == kind else reference)
            minutes = sizing.example_minutes(kind, preset.episode_minutes)
            each = sizing.estimate(minutes, keys, table, limits)
            examples.append({"service": kind,
                             "unit": "film" if kind == "radarr" else "episode",
                             **(each.as_dict(minutes) if each else {})})
        out.append({
            "id": preset.id,
            "name": i18n.t(f"profiles.preset.{preset.id}.name", language),
            "description": i18n.t(f"profiles.preset.{preset.id}.help", language),
            "services": list(preset.services),
            "wish": vprofiles.wish_as_dict(wish),
            "qualities": keys,
            "examples": examples,
            "notes": [i18n.t(key, language, **params)
                      for key, params in blueprint.notes],
            "problems": [i18n.t(key, language, **params)
                         for key, params in vprofiles.check(blueprint)],
        })
    return {"presets": out, "service_id": service_id,
            "basis": "library" if measured else "reference"}


@router.get("/api/profiles/current")
def current_profiles(service_id: int, request: Request,
                     _: dict = Depends(require_user)):
    """The profiles a service has now: who uses them, what they hold, what
    they will hold, and whether they can loop.

    The loop verdict is the ``profile_loop`` rule's own, run on the same
    reads, so this page and the findings can never disagree about it.
    """
    language = language_for(request)
    entry = arr_entry(request, service_id)
    snapshot = library_of(entry, "recent")
    connector = arr_for(entry)
    try:
        findings = check_profile_loop(connector, {
            "profiles": snapshot["profiles"], "items": snapshot["items"],
            "history": snapshot["recent"], "store": core.store}, core.engine.config())
    finally:
        connector.close()
    loops: dict = {}
    for finding in findings:
        loops.setdefault(finding.data.get("profile_id"), []).append(finding)

    table = snapshot["rates"]
    minutes = sizing.example_minutes(entry["kind"])
    rows = []
    for profile in snapshot["profiles"]:
        pid = profile.get("id")
        keys, cutoff = sizing.profile_keys(profile)
        limits = sizing.profile_limits(profile, snapshot["formats"], keys)
        chosen = [t for t in snapshot["titles"] if t.profile_id == pid]
        upgrade = bool(profile.get("upgradeAllowed"))
        own = sizing.profile_rate(chosen)
        result = sizing.library(chosen, keys, cutoff, table, limits,
                                upgrade=upgrade, own=own)
        each = sizing.estimate(minutes, keys, table, limits, own)
        found = loops.get(pid, [])
        rows.append({
            "id": pid, "name": profile.get("name"),
            "written_here": vprofiles.written_here(profile),
            "upgrade_allowed": upgrade,
            "cutoff": cutoff, "qualities": keys,
            "min_format_score": int(profile.get("minFormatScore") or 0),
            "cutoff_format_score": int(profile.get("cutoffFormatScore") or 0),
            "titles": len(chosen),
            "with_files": sum(1 for t in chosen if t.files),
            "size_on_disk_gb": result["current_gb"],
            "expected": result["expected"], "change": result["change"],
            "counts": result["counts"],
            "example": each.as_dict(minutes) if each else None,
            "own_rate": own.as_dict() if own else None,
            "loop": {
                "risk": ("error" if any(f.severity == "error" for f in found)
                         else "warning" if found else "none"),
                "problems": [{"problem": f.data.get("problem"),
                              "severity": f.severity,
                              "text": f.describe(language)} for f in found],
            },
        })
    return {"service": service_summary(entry), "profiles": rows,
            "qualities": [rate.as_dict() for rate in table.values()],
            "free_gb": sizing.free_space(snapshot["roots"], snapshot["titles"])}


class AssignBody(BaseModel):
    service_id: int
    profile_id: int
    titles: list[int] = Field(default_factory=list, max_length=20000)
    #: Without it nothing is changed, only shown.
    apply: bool = False


@router.post("/api/profiles/assign")
def assign_profile(body: AssignBody, request: Request,
                   _: dict = Depends(require_user)):
    """Put titles on another profile — shown first, done only when asked.

    Nothing is searched. The titles simply belong to the other profile from
    then on, and what it wants arrives the way anything does, when an indexer
    offers it; the preview says which files that would replace and what it
    does to the disk. Moving them back is the same call again.
    """
    language = language_for(request)
    if not body.titles:
        raise fail(request, 400, "error.nothing_selected")
    entry = arr_entry(request, body.service_id)
    snapshot = library_of(entry)
    profile = next((p for p in snapshot["profiles"]
                    if p.get("id") == body.profile_id), None)
    if profile is None:
        raise fail(request, 404, "profiles.problem.no_such_profile")
    wanted = set(body.titles)
    chosen = [t for t in snapshot["titles"] if t.id in wanted]
    moving = [t.id for t in chosen if t.profile_id != body.profile_id]

    keys, cutoff = sizing.profile_keys(profile)
    limits = sizing.profile_limits(profile, snapshot["formats"], keys)
    # What the profile's own files weigh, where it has enough of them, is a
    # better guess for a newcomer than its ladder.
    own = sizing.profile_rate([t for t in snapshot["titles"]
                               if t.profile_id == body.profile_id])
    result = sizing.library(chosen, keys, cutoff, snapshot["rates"], limits,
                            upgrade=bool(profile.get("upgradeAllowed")),
                            detail=True, own=own)
    free = sizing.free_space(snapshot["roots"], chosen)
    out = {"dry_run": not body.apply,
           "service": service_summary(entry),
           "profile": {"id": profile.get("id"), "name": profile.get("name")},
           "moving": len(moving), "unknown": len(wanted) - len(chosen),
           **result, "free_gb": free, "fits": sizing.fits(result["change"], free),
           "moved": 0}
    if body.apply and moving:
        connector = arr_for(entry)
        try:
            connector.set_quality_profile(moving, body.profile_id)
        except ArrError as e:
            raise HTTPException(502, str(e)) from e
        finally:
            connector.close()
        forget_libraries()
        out["moved"] = len(moving)
        log.info("Moved %d titles on %s to the profile %s", len(moving),
                 entry["name"], profile.get("name"))
    out["summary"] = i18n.t("storage.assign_summary", language,
                            moving=len(moving), replaced=result["counts"]["replaced"],
                            change=f"{result['change']['typical_gb']:+.0f}")
    return out


@router.get("/api/profiles/titles")
def profile_titles(service_id: int, request: Request, _: dict = Depends(require_user)):
    """Every title of a service with the profile it is on — what the page picks
    from before it asks :func:`assign_profile` to move any of them.

    From the same cached read as the size questions, so opening the picker
    right after the estimate costs nothing.
    """
    entry = arr_entry(request, service_id)
    snapshot = library_of(entry)
    return {"service": service_summary(entry),
            "profiles": [{"id": p.get("id"), "name": p.get("name")}
                         for p in snapshot["profiles"]],
            "titles": [{"id": t.id, "title": t.title, "year": t.year,
                        "profile_id": t.profile_id, "files": t.files,
                        "gb": round(t.size / sizing.GB, 1)}
                       for t in sorted(snapshot["titles"],
                                       key=lambda t: (t.title or "").lower())]}
